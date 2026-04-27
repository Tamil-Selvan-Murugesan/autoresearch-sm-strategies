"""Core backtesting engine for single-timeframe strategies (5min, daily, weekly)."""

import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from config import CFG
from state import StrategyDef

DB_PATH = CFG["db_path"]
DATA_DIRS = CFG["data_dirs"]
PUBLISH_ENABLED = CFG.get("publish_to_github", True)

# Sanity gate on per-horizon mean forward return. Anything beyond this is
# physically impossible on an index over any reasonable window — it's a sign
# the strategy's forward-return calc is broken (classic case: shift() applied
# to the filtered DataFrame instead of the full one). Offending horizons are
# dropped from `results` with a warning; the strategy still counts in
# `attempts` so the planner memory captures it.
_MAX_SANE_ABS_MEAN_PCT = 20.0

_prefix = CFG["branch_prefix"]
BRANCH_MAP = {tf: f"{_prefix}/{tf}" for tf in CFG["timeframes"]}
BRANCH_MAP["mixed"] = f"{_prefix}/mixed"

_git_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Git helper
# ---------------------------------------------------------------------------


def _run_git(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
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


def _add_col(conn: sqlite3.Connection, table: str, col: str, ddl: str) -> bool:
    """Idempotent ALTER TABLE ADD COLUMN that tolerates concurrent callers.

    Returns True if this call actually added the column, False if it already
    existed. Swallows the "duplicate column" OperationalError that happens
    when two connections race the migration.
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if col in cols:
        return False
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        return True
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            return False
        raise


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

    # `attempts` is the planner memory: one row per generated strategy, whether
    # it ran successfully, returned zero signals, or crashed. Planner nodes read
    # from here to avoid regenerating what the LLM has already tried.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS attempts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            timeframe  TEXT NOT NULL,
            strategy   TEXT NOT NULL,
            params     TEXT NOT NULL,
            status     TEXT NOT NULL,   -- 'ok' | 'zero_signals' | 'error'
            signals    INTEGER,
            error      TEXT
        );
    """)

    # Migrate: add new columns to existing tables. `_add_col` tolerates races
    # between concurrent connections (exec_* nodes migrate in parallel) — the
    # PRAGMA check isn't atomic with the ALTER, so two threads can both decide
    # to add the column, and the second one raises "duplicate column".
    _add_col(conn, "runs", "iteration", "INTEGER")
    # `runs.strategies` = JSON array of {name, params, code} for every strategy
    # in the batch. Only persisted copy of the LLM-generated source (the
    # strategies/strat_<tf>.py file is overwritten each iteration).
    _add_col(conn, "runs", "strategies", "TEXT")
    added_timeframe = _add_col(conn, "results", "timeframe", "TEXT")
    if added_timeframe:
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
        CREATE INDEX IF NOT EXISTS idx_attempts_timeframe ON attempts(timeframe);
        CREATE INDEX IF NOT EXISTS idx_attempts_strategy ON attempts(strategy);
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
    strategy_defs: list[StrategyDef] | None = None,
) -> int:
    """Write results to SQLite. Returns the run_id.

    Args:
        data_from/data_to: Full date range of the source data (not signal dates).
                           Pass these from the loaded DataFrame to get accurate ranges.
        iteration: Main loop iteration number, or None for refinement runs.
        strategy_defs: LLM-generated strategy defs (name/params/code). Serialized
                       as JSON on the runs row so each result row can be traced
                       back to its source code after the generated .py file is
                       overwritten by the next batch.
    """
    run_time = datetime.now().isoformat()

    strategies_json = None
    if strategy_defs:
        strategies_json = json.dumps(
            [
                {"name": s["name"], "params": s.get("params", {}), "code": s.get("code", "")}
                for s in strategy_defs
            ]
        )

    conn = sqlite3.connect(DB_PATH)
    _ensure_db(conn)

    cur = conn.execute(
        """INSERT INTO runs (run_at, iteration, index_name, timeframe, data_from, data_to, strategies)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (run_time, iteration, index, timeframe, data_from, data_to, strategies_json),
    )
    run_id = cur.lastrowid

    for name, (params, result) in zip(strategy_names, run_results):
        fwd_cols = [c for c in result.columns if c.startswith("fwd_")]
        params_json = json.dumps(params)
        for col in fwd_cols:
            s = summarize(result[col])
            if not s:
                continue
            if abs(s["mean"]) > _MAX_SANE_ABS_MEAN_PCT:
                print(
                    f"[WARN] {timeframe}/{name}/{col}: |mean|={s['mean']}% > "
                    f"{_MAX_SANE_ABS_MEAN_PCT}% — skipping (likely forward-return bug)"
                )
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
    """Return number of distinct strategies attempted for a timeframe.

    Counts every LLM-generated attempt (success, zero-signals, or error) so
    the main loop still terminates when the LLM produces buggy code. Use
    `attempts`, not `results`, because failed strategies leave no rows in
    `results` (their DataFrames are empty).
    """
    conn = sqlite3.connect(DB_PATH)
    _ensure_db(conn)
    cur = conn.execute(
        "SELECT COUNT(DISTINCT strategy) FROM attempts WHERE timeframe = ?",
        (timeframe,),
    )
    count = cur.fetchone()[0]
    conn.close()
    return count


# ---------------------------------------------------------------------------
# Planner memory (attempts)
# ---------------------------------------------------------------------------


def log_attempt(
    timeframe: str,
    strategy: str,
    params: dict,
    status: str,
    signals: int | None = None,
    error: str | None = None,
) -> None:
    """Record one generated strategy — whether it ran, returned 0 signals, or crashed.

    Planners read this back as memory so the LLM doesn't regenerate the same
    strategy. `status` ∈ {'ok', 'zero_signals', 'error'}.
    """
    conn = sqlite3.connect(DB_PATH)
    _ensure_db(conn)
    conn.execute(
        """INSERT INTO attempts (created_at, timeframe, strategy, params, status, signals, error)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now().isoformat(), timeframe, strategy, json.dumps(params), status, signals, error),
    )
    conn.commit()
    conn.close()


def get_attempts(timeframe: str, limit: int = 500) -> pd.DataFrame:
    """Return prior attempts for a timeframe, most recent first (for LLM memory)."""
    conn = sqlite3.connect(DB_PATH)
    _ensure_db(conn)
    df = pd.read_sql_query(
        """SELECT strategy, params, status, signals, error
           FROM attempts WHERE timeframe = ?
           ORDER BY created_at DESC LIMIT ?""",
        conn,
        params=(timeframe, limit),
    )
    conn.close()
    return df


# ---------------------------------------------------------------------------
# Query winners
# ---------------------------------------------------------------------------


def query_winners(
    min_win_rate: float = 60.0,
    min_abs_mean: float = 0.30,
    min_signals: int = 30,
    timeframe: str | None = None,
    limit: int = 50,
) -> pd.DataFrame:
    """Find strategies with directional consistency AND significant average movement.

    Long winner : win_rate >= min_win_rate        AND mean >=  min_abs_mean
    Short winner: win_rate <= (100 - min_win_rate) AND mean <= -min_abs_mean

    Expectancy is still selected for context but is no longer part of the filter.
    """
    short_win_rate_max = 100.0 - min_win_rate
    neg_abs_mean = -min_abs_mean

    conn = sqlite3.connect(DB_PATH)
    _ensure_db(conn)
    base_query = """
        SELECT r.run_at, r.iteration, r.index_name, res.timeframe,
               res.strategy, res.params, res.horizon, res.signals,
               res.mean, res.median, res.std,
               res.win_rate, res.avg_win, res.avg_loss, res.expectancy,
               CASE WHEN res.mean >= 0 THEN 'long' ELSE 'short' END AS direction
        FROM results res JOIN runs r ON r.run_id = res.run_id
        WHERE res.signals >= ?
          AND (
                (res.win_rate >= ? AND res.mean >=  ?) OR
                (res.win_rate <= ? AND res.mean <=  ?)
              )
    """
    sql_params: list = [min_signals, min_win_rate, min_abs_mean, short_win_rate_max, neg_abs_mean]
    if timeframe:
        base_query += " AND res.timeframe = ?"
        sql_params.append(timeframe)
    base_query += " ORDER BY ABS(res.mean) DESC LIMIT ?"
    sql_params.append(limit)

    df = pd.read_sql_query(base_query, conn, params=tuple(sql_params))
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
    """Archive DB + generated strategy file to the experiments branch for this timeframe.

    Uses git plumbing so the current HEAD and working tree are NEVER touched —
    the user's code branch (e.g. main) stays clean. Each publish builds a fresh
    tree containing only the files we want to archive, creates a commit via
    `git commit-tree` parented on the remote branch's current tip (or orphaned
    if the branch doesn't exist yet), and pushes that commit directly.

    Skipped entirely when `publish_to_github: false` in config.yaml.
    """
    if not PUBLISH_ENABLED:
        print(f"[publish/{timeframe}] disabled (publish_to_github=false), skipping.")
        return

    branch = BRANCH_MAP[timeframe]
    remote_ref = f"refs/heads/{branch}"

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

    strat_name = "strat_mixed.py" if timeframe == "mixed" else f"strat_{timeframe}.py"
    files_to_archive = [DB_PATH, f"strategies/{strat_name}"]
    files_to_archive = [p for p in files_to_archive if Path(p).exists()]
    if not files_to_archive:
        return

    with _git_lock:
        # 1. Build a tree in an isolated temp index — no impact on the real index.
        tmp_dir = tempfile.mkdtemp(prefix="publish-index-")
        tmp_index = str(Path(tmp_dir) / "index")
        try:
            env = os.environ.copy()
            env["GIT_INDEX_FILE"] = tmp_index
            _run_git("add", "--", *files_to_archive, env=env)
            tree_sha = _run_git("write-tree", env=env).stdout.strip()
        finally:
            Path(tmp_index).unlink(missing_ok=True)
            Path(tmp_dir).rmdir()

        if not tree_sha:
            print(f"[WARN] publish({timeframe}): empty tree, skipping")
            return

        # 2. Find the remote branch's current tip to parent onto (if it exists).
        ls = _run_git("ls-remote", "origin", remote_ref)
        parent_sha = ls.stdout.split()[0] if ls.stdout.strip() else None

        # 3. Build the commit object.
        commit_args = ["commit-tree", tree_sha, "-m", msg]
        if parent_sha:
            commit_args += ["-p", parent_sha]
        commit_sha = _run_git(*commit_args).stdout.strip()
        if not commit_sha:
            print(f"[WARN] publish({timeframe}): commit-tree failed, skipping")
            return

        # 4. Push the commit directly to the remote branch.
        _run_git("push", "origin", f"{commit_sha}:{remote_ref}")


# ---------------------------------------------------------------------------
# Execute LLM-generated strategies
# ---------------------------------------------------------------------------


def execute_strategies(
    index: str,
    timeframe: str,
    strategy_defs: list[StrategyDef],
) -> tuple[list[tuple[dict, pd.DataFrame]], str, str]:
    """Write LLM-generated strategies to a file and execute each in isolation.

    Each strategy is exec'd in its own namespace so a syntax error, NameError,
    or runtime exception in one cannot affect its siblings. Every attempt is
    recorded in the `attempts` table for planner memory.

    Returns:
        (results, data_from, data_to) — results list plus the full data date range.
    """
    # 1. Write the consolidated strategy file (for inspection / archival only).
    #    Execution happens via per-strategy exec(), not a module import, so that
    #    a single broken strategy can't kill the whole batch at import time.
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

    # 2. Load data once
    df = load(index=index, timeframe=timeframe)
    data_from = str(df["date"].min().date()) if not df.empty else None
    data_to = str(df["date"].max().date()) if not df.empty else None

    # 3. Execute each strategy in an isolated namespace.
    results = []
    for sdef in strategy_defs:
        params, result_df, status, error = _exec_single(sdef, [df])
        results.append((params, result_df))
        log_attempt(
            timeframe=timeframe,
            strategy=sdef["name"],
            params=sdef.get("params", {}),
            status=status,
            signals=len(result_df) if not result_df.empty else 0,
            error=error,
        )

    return results, data_from, data_to


def _exec_single(
    sdef: StrategyDef,
    dfs: list[pd.DataFrame],
) -> tuple[dict, pd.DataFrame, str, str | None]:
    """Compile + run one strategy in an isolated namespace.

    Returns (params, result_df, status, error) where status ∈ {'ok',
    'zero_signals', 'error'}. Never raises — all exceptions are caught so
    one bad strategy never crashes the batch.
    """
    namespace: dict = {"pd": pd, "np": np}
    try:
        exec(sdef["code"], namespace)
        fn = namespace.get(sdef["name"])
        if not callable(fn):
            raise RuntimeError(
                f"function `{sdef['name']}` not defined in generated code"
            )
        params, result_df = fn(*dfs)
        if not isinstance(result_df, pd.DataFrame):
            raise TypeError(
                f"expected DataFrame, got {type(result_df).__name__}"
            )
        status = "zero_signals" if result_df.empty else "ok"
        return params, result_df, status, None
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"[WARN] Strategy {sdef['name']} failed: {err}")
        # Full traceback to stderr for debugging — keep console output short.
        traceback.print_exc()
        return sdef.get("params", {}), pd.DataFrame(), "error", err
