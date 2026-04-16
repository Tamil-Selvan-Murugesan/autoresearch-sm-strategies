"""Core backtesting engine for single-timeframe strategies (5min, daily, weekly)."""

import importlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd

from config import CFG
from state import StrategyDef

DB_PATH = CFG["db_path"]
DATA_DIRS = CFG["data_dirs"]

_prefix = CFG["branch_prefix"]
BRANCH_MAP = {tf: f"{_prefix}/{tf}" for tf in CFG["timeframes"]}
BRANCH_MAP["mixed"] = f"{_prefix}/mixed"

_git_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Git helper
# ---------------------------------------------------------------------------


def _run_git(*args: str) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"[WARN] git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load(index: str = "NIFTY", timeframe: str = "daily") -> pd.DataFrame:
    """Load OHLCV data for a given index and timeframe."""
    data_dir = DATA_DIRS[timeframe]
    path = f"{data_dir}/{index}_{timeframe}.parquet"
    df = pd.read_parquet(path)
    return df.sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def _ensure_db(conn: sqlite3.Connection) -> None:
    """Create tables if fresh DB, then migrate columns for existing DBs."""
    # Create tables (IF NOT EXISTS — safe on existing DBs)
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
    """)

    # Migrate: add new columns to existing tables
    run_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "iteration" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN iteration INTEGER")

    res_cols = {row[1] for row in conn.execute("PRAGMA table_info(results)").fetchall()}
    if "timeframe" not in res_cols:
        conn.execute("ALTER TABLE results ADD COLUMN timeframe TEXT")
        # Backfill from runs table for old rows
        conn.execute("""
            UPDATE results SET timeframe = (
                SELECT r.timeframe FROM runs r WHERE r.run_id = results.run_id
            ) WHERE timeframe IS NULL
        """)

    # Create indexes (safe to run always — IF NOT EXISTS)
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_results_expectancy ON results(expectancy);
        CREATE INDEX IF NOT EXISTS idx_results_win_rate ON results(win_rate);
        CREATE INDEX IF NOT EXISTS idx_results_strategy ON results(strategy);
        CREATE INDEX IF NOT EXISTS idx_results_timeframe ON results(timeframe);
        CREATE INDEX IF NOT EXISTS idx_runs_timeframe ON runs(timeframe);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Summarize
# ---------------------------------------------------------------------------


def summarize(returns: pd.Series) -> dict:
    """Compute summary statistics for a series of forward returns."""
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


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log_run(
    index: str,
    timeframe: str,
    strategy_names: list[str],
    run_results: list[tuple[dict, pd.DataFrame]],
    iteration: int | None = None,
    data_from: str | None = None,
    data_to: str | None = None,
) -> int:
    """Write results to SQLite. Returns the run_id.

    Args:
        data_from/data_to: Full date range of the source data (not signal dates).
                           Pass these from the loaded DataFrame to get accurate ranges.
        iteration: Main loop iteration number, or None for refinement runs.
    """
    run_time = datetime.now().isoformat()

    conn = sqlite3.connect(DB_PATH)
    _ensure_db(conn)

    cur = conn.execute(
        """INSERT INTO runs (run_at, iteration, index_name, timeframe, data_from, data_to)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (run_time, iteration, index, timeframe, data_from, data_to),
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
                   (run_id, timeframe, strategy, params, signals, horizon, count, mean, median,
                    std, min, max, win_rate, avg_win, avg_loss, expectancy)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    timeframe,
                    name,
                    params_json,
                    len(result),
                    col,
                    s["count"],
                    s["mean"],
                    s["median"],
                    s["std"],
                    s["min"],
                    s["max"],
                    s["win_rate"],
                    s["avg_win"],
                    s["avg_loss"],
                    s["expectancy"],
                ),
            )

    conn.commit()
    conn.close()
    return run_id


# ---------------------------------------------------------------------------
# Count strategies
# ---------------------------------------------------------------------------


def count_strategies(timeframe: str) -> int:
    """Return number of distinct strategies logged for a timeframe."""
    conn = sqlite3.connect(DB_PATH)
    _ensure_db(conn)
    cur = conn.execute(
        "SELECT COUNT(DISTINCT strategy) FROM results WHERE timeframe = ?",
        (timeframe,),
    )
    count = cur.fetchone()[0]
    conn.close()
    return count


# ---------------------------------------------------------------------------
# Query winners
# ---------------------------------------------------------------------------


def query_winners(
    min_abs_expectancy: float = 0.15,
    min_signals: int = 30,
    timeframe: str | None = None,
    limit: int = 50,
) -> pd.DataFrame:
    """Find strategies with strong edge in either direction (long or short)."""
    conn = sqlite3.connect(DB_PATH)
    _ensure_db(conn)
    base_query = """
        SELECT r.run_at, r.iteration, r.index_name, res.timeframe,
               res.strategy, res.params, res.horizon, res.signals,
               res.mean, res.median, res.std,
               res.win_rate, res.avg_win, res.avg_loss, res.expectancy,
               CASE WHEN res.expectancy >= 0 THEN 'long' ELSE 'short' END AS direction
        FROM results res JOIN runs r ON r.run_id = res.run_id
        WHERE ABS(res.expectancy) >= ? AND res.signals >= ?
    """
    if timeframe:
        base_query += " AND res.timeframe = ?"
        base_query += " ORDER BY ABS(res.expectancy) DESC LIMIT ?"
        df = pd.read_sql_query(
            base_query,
            conn,
            params=(min_abs_expectancy, min_signals, timeframe, limit),
        )
    else:
        base_query += " ORDER BY ABS(res.expectancy) DESC LIMIT ?"
        df = pd.read_sql_query(
            base_query,
            conn,
            params=(min_abs_expectancy, min_signals, limit),
        )
    conn.close()
    return df


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def publish(
    timeframe: str,
    strategy_names: list[str],
    run_results: list[tuple[dict, pd.DataFrame]],
) -> None:
    """Stage, commit with searchable message, and push to a per-timeframe branch."""
    branch = BRANCH_MAP[timeframe]

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

    with _git_lock:
        _run_git("add", "logs/backtest.db", "strategies/")
        _run_git("commit", "-m", msg)
        _run_git("push", "-u", "origin", f"HEAD:refs/heads/{branch}")


# ---------------------------------------------------------------------------
# Execute LLM-generated strategies
# ---------------------------------------------------------------------------


def execute_strategies(
    index: str,
    timeframe: str,
    strategy_defs: list[StrategyDef],
) -> tuple[list[tuple[dict, pd.DataFrame]], str, str]:
    """Write LLM-generated strategies to a file, import, and execute them.

    Returns:
        (results, data_from, data_to) — results list plus the full data date range.
    """
    # 1. Build the strategy file
    strat_file = Path(f"strategies/strat_{timeframe}.py")
    strat_file.parent.mkdir(exist_ok=True)

    file_lines = [
        "# AUTO-GENERATED by planner node. This file is overwritten each iteration.",
        "# Do not edit manually — changes will be lost.",
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

    # Capture the full data date range (not signal dates)
    data_from = str(df["date"].min().date()) if not df.empty else None
    data_to = str(df["date"].max().date()) if not df.empty else None

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

    return results, data_from, data_to
